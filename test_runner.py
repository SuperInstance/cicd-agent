"""
Test Execution Engine
======================
Run tests and capture structured results with trend tracking.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fleet.cicd.test")


@dataclass
class TestResult:
    """Structured result from a test run."""
    command: str = ""
    cwd: str = ""
    exit_code: int = -1
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    timestamp: str = ""
    tests: list[dict] = field(default_factory=list)  # individual test results

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "total": self.total,
            "duration": round(self.duration, 3),
            "stdout": self.stdout[:2000],
            "stderr": self.stderr[:2000],
            "timestamp": self.timestamp,
            "tests": self.tests,
        }

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and self.failed == 0


class TestRunner:
    """Run tests and capture structured results.

    Executes test commands via subprocess, parses output, and tracks
    historical results for trend analysis.

    Usage::

        runner = TestRunner(timeout=120)
        result = runner.run("python3 -m pytest tests/ -q", cwd="/path/to/repo")
        print(f"Passed: {result.passed}, Failed: {result.failed}")
    """

    # Patterns for parsing pytest summary output
    # e.g. "5 passed, 2 failed, 1 skipped in 1.23s"
    PYTEST_SUMMARY_RE = re.compile(
        r"(\d+) (?:passed|PASSED)"
        r"(?:,?\s*(\d+) (?:failed|FAILED))?"
        r"(?:,?\s*(\d+) (?:skipped|SKIPPED))?"
        r"(?:,?\s*(\d+) (?:error|ERROR))?"
        r".*?in\s+([\d.]+)s",
        re.DOTALL,
    )

    def __init__(self, timeout: int = 120, workers: int = 2):
        self.timeout = timeout
        self.workers = workers
        self.history: list[TestResult] = []
        self._lock = threading.Lock()

    def run(self, command: str, cwd: str = ".",
            env: dict = None) -> TestResult:
        """Execute a test command and capture structured results.

        Args:
            command: Shell command to run (e.g. ``python3 -m pytest tests/ -q``).
            cwd: Working directory for the command.
            env: Optional environment variable overrides.

        Returns:
            A ``TestResult`` with parsed output.
        """
        logger.info("Running tests: %s  (cwd=%s)", command, cwd)
        start = time.time()
        merged_env = None
        if env:
            import os
            merged_env = {**os.environ, **env}

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=merged_env,
            )
            stdout = proc.stdout
            stderr = proc.stderr
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            stdout = ""
            stderr = f"Test command timed out after {self.timeout}s"
            exit_code = -1
            logger.error("Test timeout: %s", command)
        except Exception as e:
            stdout = ""
            stderr = f"Failed to run tests: {e}"
            exit_code = -1
            logger.error("Test execution error: %s", e)

        duration = time.time() - start
        result = self._parse_result(command, cwd, exit_code, stdout, stderr, duration)
        result.timestamp = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self.history.append(result)

        logger.info(
            "Test result: passed=%d failed=%d skipped=%d duration=%.1fs",
            result.passed, result.failed, result.skipped, duration,
        )
        return result

    def run_parallel(self, commands: list[tuple[str, str]]) -> dict[str, TestResult]:
        """Run tests for multiple repos in parallel.

        Args:
            commands: List of (repo_name, command, cwd) tuples.
                      Expects ``[(name, cmd, cwd), ...]`` format
                      but also accepts ``[(cmd, cwd), ...]`` for backwards compat.

        Returns:
            Dict mapping repo name (or index) to TestResult.
        """
        results: dict[str, TestResult] = {}
        max_workers = min(self.workers, len(commands))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, item in enumerate(commands):
                if len(item) == 3:
                    name, cmd, cwd = item
                elif len(item) == 2:
                    name, cmd, cwd = str(i), item[0], item[1]
                else:
                    continue
                future = executor.submit(self.run, cmd, cwd)
                futures[future] = name

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except Exception as e:
                    logger.error("Parallel test error for %s: %s", name, e)
                    results[name] = TestResult(
                        command="",
                        cwd="",
                        exit_code=-1,
                        stderr=str(e),
                    )
        return results

    def _parse_result(
        self,
        command: str,
        cwd: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        duration: float,
    ) -> TestResult:
        """Parse test output and construct a TestResult."""
        combined = stdout + "\n" + stderr
        result = TestResult(
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            duration=duration,
            stdout=stdout,
            stderr=stderr,
        )

        # Try pytest summary pattern
        match = self.PYTEST_SUMMARY_RE.search(combined)
        if match:
            result.passed = int(match.group(1)) if match.group(1) else 0
            result.failed = int(match.group(2)) if match.group(2) else 0
            result.skipped = int(match.group(3)) if match.group(3) else 0
            result.errors = int(match.group(4)) if match.group(4) else 0
            result.total = result.passed + result.failed + result.skipped + result.errors
            return result

        # Fallback: try to parse "N passed" pattern
        simple_match = re.search(r"(\d+)\s+passed", combined)
        if simple_match:
            result.passed = int(simple_match.group(1))

        # Look for failures
        fail_match = re.search(r"(\d+)\s+failed", combined)
        if fail_match:
            result.failed = int(fail_match.group(1))

        skip_match = re.search(r"(\d+)\s+skipped", combined)
        if skip_match:
            result.skipped = int(skip_match.group(1))

        error_match = re.search(r"(\d+)\s+error", combined, re.IGNORECASE)
        if error_match:
            result.errors = int(error_match.group(1))

        result.total = result.passed + result.failed + result.skipped + result.errors

        # Parse individual test names from FAIL/ERROR lines
        result.tests = self._parse_individual_tests(combined)

        return result

    def _parse_individual_tests(self, output: str) -> list[dict]:
        """Parse individual test names and statuses from pytest output."""
        tests = []
        # Match patterns like "FAILED test_foo.py::test_bar" or "PASSED ..."
        for line in output.split("\n"):
            for status in ("FAILED", "PASSED", "SKIPPED", "ERROR"):
                if status in line and "::" in line:
                    # Extract the test path
                    parts = line.strip().split()
                    for part in parts:
                        if "::" in part:
                            tests.append({
                                "name": part,
                                "status": status.lower(),
                            })
                            break
                    break
        return tests

    # -- Historical Analysis --

    def get_trend(self, limit: int = 10) -> dict:
        """Get trend analysis of recent test runs.

        Returns pass rate, average duration, and any flaky tests.
        """
        with self._lock:
            recent = list(self.history[-limit:])

        if not recent:
            return {"total_runs": 0}

        passed_count = sum(1 for r in recent if r.success)
        avg_duration = sum(r.duration for r in recent) / len(recent)

        # Detect flaky tests: tests that both pass and fail across runs
        test_outcomes: dict[str, dict[str, int]] = {}
        for r in recent:
            for t in r.tests:
                name = t["name"]
                status = t["status"]
                if name not in test_outcomes:
                    test_outcomes[name] = {"passed": 0, "failed": 0, "skipped": 0}
                if status in test_outcomes[name]:
                    test_outcomes[name][status] += 1

        flaky = [
            name for name, outcomes in test_outcomes.items()
            if outcomes["passed"] > 0 and outcomes["failed"] > 0
        ]

        return {
            "total_runs": len(recent),
            "passed": passed_count,
            "failed": len(recent) - passed_count,
            "pass_rate": f"{(passed_count / len(recent) * 100):.1f}%",
            "avg_duration": round(avg_duration, 2),
            "flaky_tests": flaky,
            "test_stability": {
                name: outcomes for name, outcomes in test_outcomes.items()
                if outcomes["failed"] > 0
            },
        }

    def get_history(self, limit: int = 20) -> list[dict]:
        """Get recent test results."""
        with self._lock:
            recent = list(self.history[-limit:])
        return [r.to_dict() for r in recent]

    def clear_history(self) -> None:
        """Clear historical results."""
        with self._lock:
            self.history.clear()
