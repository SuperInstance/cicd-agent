"""
Tests for Fleet CI/CD Agent
=============================
Comprehensive tests for the CI/CD pipeline engine, git poller,
test runner, reporter, webhook server, CLI, and pipeline execution.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add the parent directory to the path so we can import our modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from cicd import (
    ArtifactManager,
    DeployManager,
    FleetCICD,
    NotificationManager,
    PipelineConfig,
    PipelineRun,
    PipelineRunner,
    PipelineStatus,
    StageName,
    StageResult,
)
from git_poller import GitCommit, GitPoller
from test_runner import TestResult, TestRunner
from reporter import CIReporter
from webhook_server import WebhookServer, parse_github_push_event


# ===================================================================
# Pipeline Config Tests
# ===================================================================

class TestPipelineConfig(unittest.TestCase):
    """Test PipelineConfig dataclass."""

    def test_default_values(self):
        config = PipelineConfig(repo_name="test-agent")
        self.assertEqual(config.repo_name, "test-agent")
        self.assertEqual(config.test_command, "python3 -m pytest tests/ -q")
        self.assertEqual(config.lint_command, "python3 -m py_compile")
        self.assertFalse(config.auto_deploy)
        self.assertEqual(config.deploy_target, "local")
        self.assertEqual(config.max_retries, 2)
        self.assertEqual(config.timeout, 120)

    def test_custom_values(self):
        config = PipelineConfig(
            repo_name="custom-agent",
            test_command="python3 -m pytest -x",
            lint_command="flake8",
            auto_deploy=True,
            deploy_target="docker",
            max_retries=3,
            timeout=300,
        )
        self.assertEqual(config.repo_name, "custom-agent")
        self.assertTrue(config.auto_deploy)
        self.assertEqual(config.deploy_target, "docker")
        self.assertEqual(config.max_retries, 3)
        self.assertEqual(config.timeout, 300)

    def test_notify_on_default(self):
        config = PipelineConfig(repo_name="test")
        self.assertIn("failure", config.notify_on)
        self.assertIn("recovery", config.notify_on)

    def test_skip_patterns_default(self):
        config = PipelineConfig(repo_name="test")
        self.assertIn("[skip-ci]", config.skip_patterns)

    def test_serialization(self):
        from dataclasses import asdict
        config = PipelineConfig(repo_name="test-agent")
        d = asdict(config)
        self.assertEqual(d["repo_name"], "test-agent")
        self.assertIn("test_command", d)
        self.assertIn("notify_on", d)


# ===================================================================
# Git Poller Tests (with mock git)
# ===================================================================

class TestGitPoller(unittest.TestCase):
    """Test GitPoller with mocked git commands."""

    def setUp(self):
        self.poller = GitPoller(interval=30)

    def test_add_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Mock _get_head_sha to simulate a git repo
            self.poller._get_head_sha = lambda p, b: "abc123" * 5
            self.poller.add_repo("test-repo", tmpdir, "main")
            self.assertIn("test-repo", self.poller._repos)
            self.assertEqual(self.poller._last_known["test-repo"], "abc123" * 5)

    def test_remove_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.poller._get_head_sha = lambda p, b: "abc123" * 5
            self.poller.add_repo("test-repo", tmpdir)
            result = self.poller.remove_repo("test-repo")
            self.assertTrue(result)
            self.assertNotIn("test-repo", self.poller._repos)

    def test_remove_nonexistent_repo(self):
        result = self.poller.remove_repo("no-such-repo")
        self.assertFalse(result)

    def test_set_interval(self):
        self.poller.set_interval(120)
        self.assertEqual(self.poller.interval, 120)

    def test_set_last_known(self):
        self.poller.set_last_known("repo1", "sha123")
        self.assertEqual(self.poller.get_last_known("repo1"), "sha123")

    def test_get_last_known_missing(self):
        self.assertIsNone(self.poller.get_last_known("nonexistent"))

    def test_extract_tags(self):
        tags = self.poller._extract_tags("[skip-ci] fix typo")
        self.assertIn("skip-ci", tags)

        tags = self.poller._extract_tags("[deploy][urgent] hotfix")
        self.assertIn("deploy", tags)
        self.assertIn("urgent", tags)

        tags = self.poller._extract_tags("normal commit")
        self.assertEqual(tags, [])

    def test_poll_with_mock(self):
        """Test polling with a mocked git environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            self.poller._get_head_sha = MagicMock(side_effect=["sha_old", "sha_new"])
            self.poller._git_fetch = MagicMock(return_value=True)
            self.poller._get_new_commits = MagicMock(return_value=[
                GitCommit(sha="sha_new", author="Test", message="fix: bug fix"),
            ])

            self.poller.add_repo("mock-repo", tmpdir, "main")
            # Reset the last known so poll detects changes
            self.poller._last_known["mock-repo"] = "sha_old"

            results = self.poller.poll_all()
            self.assertIn("mock-repo", results)
            self.assertEqual(len(results["mock-repo"]), 1)
            self.assertEqual(results["mock-repo"][0].sha, "sha_new")

    def test_get_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.poller._get_head_sha = lambda p, b: "sha1"
            self.poller.add_repo("repo1", tmpdir)
            status = self.poller.get_status()
            self.assertEqual(status["interval"], 30)
            self.assertIn("repo1", status["repos"])

    def test_poll_repo_not_found(self):
        result = self.poller.poll_repo("nonexistent")
        self.assertEqual(result, [])


# ===================================================================
# Test Runner Tests (with mock pytest)
# ===================================================================

class TestTestRunner(unittest.TestCase):
    """Test TestRunner with mocked subprocess."""

    def test_run_success(self):
        """Test running a successful test command."""
        runner = TestRunner(timeout=10)
        result = runner.run("echo '3 passed' && exit 0")
        self.assertTrue(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertGreater(result.duration, 0)

    def test_run_failure(self):
        """Test running a failing test command."""
        runner = TestRunner(timeout=10)
        result = runner.run("echo '1 failed' && exit 1")
        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)

    def test_parse_pytest_output(self):
        """Test parsing pytest summary output."""
        runner = TestRunner()
        stdout = "test_foo.py ... 5 passed, 2 failed, 1 skipped in 1.23s"
        result = runner._parse_result("pytest", ".", 1, stdout, "", 1.5)
        self.assertEqual(result.passed, 5)
        self.assertEqual(result.failed, 2)
        self.assertEqual(result.skipped, 1)

    def test_parse_simple_passed(self):
        """Test parsing simple 'N passed' output."""
        runner = TestRunner()
        stdout = "10 passed"
        result = runner._parse_result("pytest", ".", 0, stdout, "", 0.5)
        self.assertEqual(result.passed, 10)
        self.assertEqual(result.failed, 0)

    def test_parse_no_output(self):
        """Test parsing empty output."""
        runner = TestRunner()
        result = runner._parse_result("pytest", ".", 0, "", "", 0.1)
        self.assertEqual(result.passed, 0)
        self.assertEqual(result.failed, 0)

    def test_timeout_handling(self):
        """Test that timeout is handled gracefully."""
        runner = TestRunner(timeout=1)
        result = runner.run("sleep 10", cwd=".")
        self.assertEqual(result.exit_code, -1)
        self.assertIn("timed out", result.stderr)

    def test_result_to_dict(self):
        """Test TestResult serialization."""
        result = TestResult(
            command="pytest",
            cwd="/tmp",
            exit_code=0,
            passed=5,
            failed=0,
            skipped=1,
            duration=1.5,
        )
        d = result.to_dict()
        self.assertEqual(d["passed"], 5)
        self.assertEqual(d["failed"], 0)
        self.assertEqual(d["skipped"], 1)
        self.assertEqual(d["exit_code"], 0)
        self.assertIn("command", d)

    def test_history_tracking(self):
        """Test that runs are tracked in history."""
        runner = TestRunner()
        runner.run("echo '1 passed' && exit 0")
        runner.run("echo '2 passed' && exit 0")
        history = runner.get_history()
        self.assertEqual(len(history), 2)

    def test_clear_history(self):
        """Test clearing history."""
        runner = TestRunner()
        runner.run("echo '1 passed' && exit 0")
        runner.clear_history()
        self.assertEqual(len(runner.get_history()), 0)

    def test_trend_analysis(self):
        """Test trend analysis."""
        runner = TestRunner()
        runner.run("echo '5 passed' && exit 0")
        runner.run("echo '5 passed' && exit 0")
        trend = runner.get_trend()
        self.assertEqual(trend["total_runs"], 2)
        self.assertEqual(trend["passed"], 2)

    def test_parallel_run(self):
        """Test parallel test execution."""
        runner = TestRunner(workers=2)
        commands = [
            ("repo1", "echo '1 passed' && exit 0", "."),
            ("repo2", "echo '2 passed' && exit 0", "."),
        ]
        results = runner.run_parallel(commands)
        self.assertIn("repo1", results)
        self.assertIn("repo2", results)
        self.assertTrue(results["repo1"].success)
        self.assertTrue(results["repo2"].success)

    def test_parallel_failure(self):
        """Test parallel execution with a failure."""
        runner = TestRunner(workers=2)
        commands = [
            ("repo1", "echo '1 passed' && exit 0", "."),
            ("repo2", "echo 'failed' && exit 1", "."),
        ]
        results = runner.run_parallel(commands)
        self.assertTrue(results["repo1"].success)
        self.assertFalse(results["repo2"].success)


# ===================================================================
# Reporter Tests
# ===================================================================

class TestCIReporter(unittest.TestCase):
    """Test CI/CD report generation."""

    def setUp(self):
        self.reporter = CIReporter()
        self.run = PipelineRun(
            run_id="test-20250101-120000",
            repo_name="test-agent",
            status=PipelineStatus.PASSED,
            trigger="poll",
            commit_sha="a" * 40,
            commit_message="fix: resolve bug in handler",
            branch="main",
            total_duration=15.5,
            stages=[
                StageResult(
                    stage=StageName.TEST,
                    status=PipelineStatus.PASSED,
                    duration=5.0,
                    message="Tests: 10 passed, 0 failed",
                    data={"passed": 10, "failed": 0, "skipped": 1, "errors": 0, "total": 11},
                ),
                StageResult(
                    stage=StageName.LINT,
                    status=PipelineStatus.PASSED,
                    duration=2.0,
                    message="Linted 25 files, 0 errors",
                ),
                StageResult(
                    stage=StageName.VALIDATE,
                    status=PipelineStatus.PASSED,
                    duration=0.1,
                    message="All checks passed",
                ),
            ],
        )

    def test_json_report(self):
        report = self.reporter.generate_json(self.run)
        self.assertEqual(report["run_id"], "test-20250101-120000")
        self.assertEqual(report["repo_name"], "test-agent")
        self.assertEqual(report["status"], "passed")
        self.assertIn("_report_meta", report)
        self.assertEqual(report["_report_meta"]["format"], "json")

    def test_text_report(self):
        report = self.reporter.generate_text(self.run)
        self.assertIn("FLEET CI/CD REPORT", report)
        self.assertIn("test-agent", report)
        self.assertIn("PASSED", report)
        self.assertIn("test", report)
        self.assertIn("10 passed", report)

    def test_markdown_report(self):
        report = self.reporter.generate_markdown(self.run)
        self.assertIn("# CI/CD Report", report)
        self.assertIn("test-agent", report)
        self.assertIn("Pipeline Stages", report)
        self.assertIn("Test Results", report)
        self.assertIn("Fleet CI/CD Agent", report)

    def test_text_report_failed(self):
        self.run.status = PipelineStatus.FAILED
        report = self.reporter.generate_text(self.run)
        self.assertIn("FAILED", report)

    def test_trend_report(self):
        runs = [self.run]
        trend = self.reporter.generate_trend(runs)
        self.assertEqual(trend["total_runs"], 1)
        self.assertEqual(trend["passed"], 1)
        self.assertEqual(trend["pass_rate"], "100.0%")

    def test_trend_empty(self):
        trend = self.reporter.generate_trend([])
        self.assertEqual(trend["total_runs"], 0)

    def test_trend_multiple(self):
        passed_run = PipelineRun(
            run_id="p1", repo_name="test",
            status=PipelineStatus.PASSED, total_duration=10.0,
        )
        failed_run = PipelineRun(
            run_id="f1", repo_name="test",
            status=PipelineStatus.FAILED, total_duration=5.0,
        )
        trend = self.reporter.generate_trend([passed_run, failed_run])
        self.assertEqual(trend["total_runs"], 2)
        self.assertEqual(trend["pass_rate"], "50.0%")
        self.assertEqual(trend["avg_duration"], 7.5)

    def test_report_from_dict(self):
        """Test generating report from a plain dict."""
        data = {"run_id": "d1", "repo_name": "dict-repo", "status": "passed"}
        report = self.reporter.generate_json(data)
        self.assertEqual(report["run_id"], "d1")

    def test_status_emoji(self):
        self.assertEqual(CIReporter._status_emoji("passed"), "✅")
        self.assertEqual(CIReporter._status_emoji("failed"), "❌")
        self.assertEqual(CIReporter._status_emoji("running"), "🔄")
        self.assertEqual(CIReporter._status_emoji("unknown"), "  ")


# ===================================================================
# Webhook Parsing Tests
# ===================================================================

class TestWebhookParsing(unittest.TestCase):
    """Test webhook event parsing."""

    def test_parse_push_event(self):
        payload = {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": "fleet/agent-1", "name": "agent-1"},
            "commits": [
                {
                    "id": "sha1",
                    "author": {"name": "Developer"},
                    "message": "fix: bug fix [skip-ci]",
                    "added": ["new_file.py"],
                    "modified": ["existing.py"],
                    "removed": [],
                }
            ],
        }
        result = parse_github_push_event(payload)
        self.assertEqual(result["repo"], "fleet/agent-1")
        self.assertEqual(result["branch"], "main")
        self.assertEqual(result["after"], "abc123")
        self.assertEqual(len(result["commits"]), 1)
        self.assertEqual(result["commits"][0]["sha"], "sha1")
        self.assertIn("new_file.py", result["changed_files"])
        self.assertIn("existing.py", result["changed_files"])

    def test_parse_push_no_commits(self):
        payload = {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": "fleet/agent"},
            "commits": [],
        }
        result = parse_github_push_event(payload)
        self.assertEqual(len(result["commits"]), 0)
        self.assertEqual(len(result["changed_files"]), 0)

    def test_parse_non_heads_ref(self):
        payload = {
            "ref": "refs/tags/v1.0",
            "repository": {"name": "agent"},
            "commits": [],
        }
        result = parse_github_push_event(payload)
        self.assertEqual(result["branch"], "refs/tags/v1.0")

    def test_signature_verification(self):
        server = WebhookServer(secret="my-secret")
        payload = b'{"test": true}'
        sig = "sha256=" + hmac.new(b"my-secret", payload, hashlib.sha256).hexdigest()
        self.assertTrue(server.verify_signature(payload, sig))

    def test_signature_wrong_secret(self):
        server = WebhookServer(secret="my-secret")
        payload = b'{"test": true}'
        sig = "sha256=" + hmac.new(b"wrong-secret", payload, hashlib.sha256).hexdigest()
        self.assertFalse(server.verify_signature(payload, sig))

    def test_signature_no_secret(self):
        server = WebhookServer(secret="")
        payload = b'{"test": true}'
        self.assertTrue(server.verify_signature(payload, ""))

    def test_record_event(self):
        server = WebhookServer()
        server.record_event({"event": "push", "repo": "test"})
        self.assertEqual(len(server.received_events), 1)
        self.assertEqual(server.received_events[0]["event"], "push")


# ===================================================================
# CLI Arguments Tests
# ===================================================================

class TestCLIArguments(unittest.TestCase):
    """Test CLI argument parsing."""

    def setUp(self):
        # Import here to avoid side effects
        from cli import build_parser
        self.parser = build_parser()

    def test_serve_defaults(self):
        args = self.parser.parse_args(["serve"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.interval, 60)
        self.assertEqual(args.webhook_port, 9000)

    def test_serve_custom(self):
        args = self.parser.parse_args([
            "serve", "--interval", "30", "--webhook-port", "8080",
        ])
        self.assertEqual(args.interval, 30)
        self.assertEqual(args.webhook_port, 8080)

    def test_run_repo(self):
        args = self.parser.parse_args(["run", "--repo", "my-agent"])
        self.assertEqual(args.repo, "my-agent")
        self.assertFalse(args.all_flag)

    def test_run_all(self):
        args = self.parser.parse_args(["run", "--all"])
        self.assertTrue(args.all_flag)

    def test_history(self):
        args = self.parser.parse_args(["history", "--limit", "10"])
        self.assertEqual(args.command, "history")
        self.assertEqual(args.limit, 10)

    def test_report_format(self):
        args = self.parser.parse_args(["report", "--format", "json"])
        self.assertEqual(args.format, "json")

    def test_report_markdown(self):
        args = self.parser.parse_args(["report", "--format", "markdown"])
        self.assertEqual(args.format, "markdown")

    def test_webhook_serve(self):
        args = self.parser.parse_args(["webhook-serve", "--port", "7000"])
        self.assertEqual(args.port, 7000)

    def test_onboard(self):
        args = self.parser.parse_args(["onboard"])
        self.assertEqual(args.command, "onboard")

    def test_no_command(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.command)


# ===================================================================
# Pipeline Execution Flow Tests
# ===================================================================

class TestPipelineExecution(unittest.TestCase):
    """Test end-to-end pipeline execution."""

    def test_fleet_cicd_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cicd = FleetCICD(config_dir=tmpdir)
            self.assertIsNotNone(cicd.artifacts)
            self.assertIsNotNone(cicd.deploy_mgr)
            self.assertIsNotNone(cicd.notifier)

    def test_add_remove_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cicd = FleetCICD(config_dir=tmpdir)
            config = PipelineConfig(repo_name="test", repo_path=tmpdir)
            cicd.add_repo(config)
            repos = cicd.list_repos()
            self.assertEqual(len(repos), 1)
            self.assertEqual(repos[0]["name"], "test")

            cicd.remove_repo("test")
            self.assertEqual(len(cicd.list_repos()), 0)

    def test_run_repo_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cicd = FleetCICD(config_dir=tmpdir)
            result = cicd.run_repo("nonexistent")
            self.assertIsNone(result)

    def test_get_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cicd = FleetCICD(config_dir=tmpdir)
            status = cicd.get_status()
            self.assertEqual(status["repos_monitored"], 0)
            self.assertFalse(status["polling"])
            self.assertFalse(status["webhook"])

    def test_get_history_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cicd = FleetCICD(config_dir=tmpdir)
            history = cicd.get_history()
            self.assertEqual(history, [])

    def test_artifact_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = ArtifactManager(base_dir=tmpdir)
            path = artifacts.save_report("test-run", {"key": "value"}, "json")
            self.assertTrue(path.exists())
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["key"], "value")

    def test_artifact_list_reports(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifacts = ArtifactManager(base_dir=tmpdir)
            artifacts.save_report("run1", {"id": 1}, "json")
            artifacts.save_report("run2", {"id": 2}, "json")
            reports = artifacts.list_reports()
            self.assertEqual(len(reports), 2)

    def test_notification_manager(self):
        notifier = NotificationManager(notify_channels=["log"])
        config = PipelineConfig(repo_name="test")
        run = PipelineRun(
            run_id="test-run",
            repo_name="test",
            status=PipelineStatus.FAILED,
            total_duration=5.0,
        )
        notifier.notify(run, config)
        self.assertEqual(len(notifier.history), 1)

    def test_notification_skip(self):
        notifier = NotificationManager(notify_channels=["log"])
        config = PipelineConfig(repo_name="test", notify_on=["failure"])
        run = PipelineRun(
            run_id="test-run",
            repo_name="test",
            status=PipelineStatus.PASSED,
            total_duration=5.0,
        )
        # "recovery" not in notify_on, but "success" won't be checked
        notifier.notify(run, config)
        # Should not notify since only "failure" triggers
        # Actually, PASSED triggers "recovery" and "success" — notify_on has "failure" only
        self.assertEqual(len(notifier.history), 0)

    def test_deploy_manager_local(self):
        dm = DeployManager()
        with tempfile.TemporaryDirectory() as tmpdir:
            result = dm.deploy("local", tmpdir)
            self.assertTrue(result["success"])  # No install script = skip

    def test_deploy_manager_unknown(self):
        dm = DeployManager()
        result = dm.deploy("kubernetes", "/tmp")
        self.assertFalse(result["success"])
        self.assertIn("Unknown deploy target", result["message"])

    def test_pipeline_runner_skip_ci(self):
        """Test that [skip-ci] commits skip the pipeline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = PipelineConfig(repo_name="test", repo_path=tmpdir)
            artifacts = ArtifactManager(base_dir=str(Path(tmpdir) / "artifacts"))
            deploy_mgr = DeployManager()
            notifier = NotificationManager()

            runner = PipelineRunner(
                repo_path=tmpdir,
                config=config,
                artifacts=artifacts,
                deploy_mgr=deploy_mgr,
                notifier=notifier,
            )
            run = runner.run_pipeline(
                trigger="poll",
                commit_message="[skip-ci] trivial change",
            )
            self.assertEqual(run.status, PipelineStatus.SKIPPED)

    def test_pipeline_runner_no_skip(self):
        """Test that normal commits run the pipeline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = PipelineConfig(repo_name="test", repo_path=tmpdir)
            artifacts = ArtifactManager(base_dir=str(Path(tmpdir) / "artifacts"))
            deploy_mgr = DeployManager()
            notifier = NotificationManager()

            runner = PipelineRunner(
                repo_path=tmpdir,
                config=config,
                artifacts=artifacts,
                deploy_mgr=deploy_mgr,
                notifier=notifier,
            )
            run = runner.run_pipeline(
                trigger="manual",
                commit_message="fix: real bug fix",
            )
            # Pipeline should have run (may pass or fail depending on test results)
            self.assertNotEqual(run.status, PipelineStatus.SKIPPED)
            self.assertGreater(len(run.stages), 0)

    def test_stage_result_serialization(self):
        sr = StageResult(
            stage=StageName.TEST,
            status=PipelineStatus.PASSED,
            duration=1.5,
            message="All tests passed",
        )
        d = sr.to_dict()
        self.assertEqual(d["stage"], "test")
        self.assertEqual(d["status"], "passed")
        self.assertEqual(d["duration"], 1.5)

    def test_pipeline_run_serialization(self):
        run = PipelineRun(
            run_id="run-1",
            repo_name="agent-1",
            status=PipelineStatus.PASSED,
            total_duration=10.0,
            stages=[
                StageResult(stage=StageName.TEST, status=PipelineStatus.PASSED),
            ],
        )
        d = run.to_dict()
        self.assertEqual(d["run_id"], "run-1")
        self.assertEqual(d["status"], "passed")
        self.assertEqual(len(d["stages"]), 1)


# ===================================================================
# Main
# ===================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
